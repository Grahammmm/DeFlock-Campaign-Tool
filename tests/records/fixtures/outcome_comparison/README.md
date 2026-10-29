# Synthetic per-run documentary comparison fixture

All agencies, identifiers, policy sections, dates and records in fact-sheets.json
are invented. The column families (public policy, authorized users/training,
retention by data layer, sharing, audits and native access records) follow the
admitted crosswalk/catalog references. No reference narrative or real records
are copied here. Canonical table/column shapes in the integration test are from
campaign_tool/records/ledger/migrations/v001.py, not an invented database API.

## Coordinator input contract / defaults

The outputs hook reads PROJECT/agency-fact-sheets.json, never a path named by a
record. This is an OPTIONAL coordinator export with the exact schema shown in
fact-sheets.json. There is no existing fact-sheet export at the source pin.
The hook does not generate one, duplicate extraction, or write this input.
Canonical agencies and requests are unioned into the report denominator. Missing
exports/fields yield unknown; timestamps/statuses in the ledger do not prove
actual request receipt, determination, or law applicability. Other fact-sheet
fields remain explicit unknown/unmapped rows. Missing agency inventory produces
an empty report with agency_count=0, not a claim of complete agency coverage.

The coordinator/source custodian must map existing reviewed facts to this schema
and qualify it in the operator TEST run before live installation. Synthetic
success does not establish real-data compatibility or legal adequacy.

Each fact has state=observed, applicability_confirmed=true, a value object and
nonempty citations [{source_id, locator}]. Other states (missing/redacted/etc.),
unqualified scope, incomplete predicates and counterevidence remain unknown.
A source locator is retained for review, not authenticated by this projection.
The source custodian must attest that the cited record supports the exact
predicate, operative policy/version, data layer, time and agency. False must mean
an affirmative cited discrepancy, NOT failure to locate a record. Applicability
confirmation includes those temporal/version conditions. Predicates use exact
booleans, not truthiness or keywords; all required predicates must be present.

regulated_roles is a list drawn from operator,
end_user, public_agency. Jurisdiction must be us-ca. Operators and end-users have
distinct policy duties; the periodic audit-process predicate is for end-users,
native access-record predicates for operators; pre-implementation public comment
requires public_agency plus operator. Sharing-policy presence does not establish
lawful recipients or actual disclosures. Retention requires data_layer and checks
only a documented length/destruction process, never 30/60/365-day universal caps.
No arbitrary audit frequency or mandatory one-year local retention is introduced.
Policy rows are separate from retention_practice, sharing_practice and
audit_practice rows: a documented policy cannot establish matching practice.
Each practice value requires native_coverage_complete=true, a policy_version,
policy_modality=shall/should and exceptions_resolved=true. The custodian must
resolve operative dates, event scope and exceptions; an incomplete production
cannot set those confirmations. Retention practice compares observed_days to
supplied inclusive min_days/max_days only for identical data_layer and
policy_data_layer. No default bound exists. The reported reason retains should
versus shall; a recommendation mismatch is not relabeled mandatory. Sharing
practice requires event_type=disclosure and source-supported recipient/purpose/
approval comparisons; a configured relationship stays unknown. Audit practice
requires a source-supported process_followed predicate over the qualified native
scope; this projection does not invent audits or infer their absence.
meets means the cited documentary predicates match, never agency compliance.

Requests have request_id, confirmed receipt, determination_state=made/pending,
and date-only received_on/determination_on. Dates in this interface mean agency-
local civil dates, not an inferred timezone conversion from email timestamps.
The coordinator must resolve timezone/date ambiguity before receipt_confirmed.
calendar_confirmed plus calendar_start/end/holidays is an explicit custodian-
qualified calendar including applicable holidays/local rules, not an automatic
assumption that the synthetic empty calendar applies to real agencies. The
screening excludes receipt day, uses 10 calendar days, rolls a weekend/holiday
last day forward, and allows at most 14 additional days from the base due date.
extension_claimed must be explicit. If true, extension_days,
extension_notice_on and extension_notice_qualified=true are required. The latter
is a source-custodian confirmation of a reasonably necessary written notice by
the agency head/designee, enumerated unusual circumstances, stated reasons and
expected dispatch date. This tool cannot supply that legal/human conclusion.
Unqualified/invalid extension data stays unknown, never silently ignored. A
pending determination on/before due stays unknown; after due it is a documentary
timing gap. Production/acknowledgment is not determination. Any unresolved date,
calendar coverage, receipt, exception or counterevidence stays unknown.

The cited legal baseline is the existing draft jurisdictions/us-ca/package.json
and its official Legislative Information URLs, including Civil Code 1798.90.5
(scope), .51-.53 (safeguards/policies/access), .55 (public agencies) and Government
Code 7922.535/6800/6700 (CPRA timing). Civil Code .54 remedies are not adjudicated
by this output. The catalog corrects the earlier crosswalk's should/shall,
cloud/local-retention and operative-version ambiguities. No new legal research
or legal-package approval is claimed, and SB 274 is not treated as operative.

## Outputs and acceptance

run-outputs/<sanitized-run-id>/comparison.json and comparison.md are private,
review-required. Every row carries status meets/gap/unknown, a statutory citation,
needs_attorney_review=true, expected criterion, reason and any supplied factual
locators. The existing run-outputs/<run-id>.json counts snapshot and stage result
remain unchanged. Legacy count-only test schemas without agencies/requests keep
their original success result, but comparison reports explicitly mark missing
inventory tables and inventory_complete=false; this is not complete coverage.
Malformed supplied input or a report write/read failure fails
the outputs stage with existing OUTPUTS_WRITE_FAILED, never a missing report
reported as success. The canonical database is only opened using the existing
read-only immutable snapshot guard; busy snapshots retain their existing hold.
Reports are individually atomically replaced, not a two-file transaction; any
write error fails the stage. This report is not a stage receipt or publication.

Acceptance, cwd repository root:

    python3 -m unittest discover -s /home/user/agent-work/candidates/deflock/OUTCOME-C/repo/tests/records -p 'test_outcome_comparison*.py'

Tests retain unique synthetic projects under ignored build/outcome-comparison;
no cleanup/deletion of evidence, real records, network or model call is needed.
They exercise the actual stage hook, canonical schema, read-only fingerprints,
all documentary predicates, CPRA boundaries, unknown/empty/malformed inputs,
run-specific JSON/Markdown, path refusals, write errors and injected negative
controls that detect a bypassed citation gate or omitted outputs hook.
