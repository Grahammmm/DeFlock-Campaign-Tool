# WP5: bounded canonical catalog and private board candidate

This library exports owner-private, read-only candidate cards from the WP1
canonical ledger. Its separate acceptance module can transactionally select an
exact private board snapshot. Neither module closes records, promotes review,
serves a port, authenticates viewers, contacts a model or agency, or publishes.

## Dependencies and API

`campaign_tool.records.ledger_catalog.build(database, private_scratch,
overlays=None, since=None, run_id=None)` creates a consistent SQLite backup in owner-private
scratch, calls WP1 `ledger.stages.query_counts` on that disposable copy, and
projects metadata. The source is opened read-only; no WP1 source files or live
ledger are modified. WP1 migrations on the disposable copy do not activate a
live schema. `since` requires a timezone and describes ledger arrivals, not
mailbox export coverage.

`export(catalog, private_output)` writes an immutable hash-named directory with
catalog.json, counts.json, index.html and artifact-hashes.json. Replays verify
exact bytes and never overwrite. The directory must already exist with owner
mode 0700 outside every Git checkout; artifacts are 0600. Canonical absolute
paths are required; symlinks and parent traversal are rejected. Export locking
and directory/file fsync protect concurrent writers and retry boundaries.
No accepted pointer is moved.

`project(connection, counts, overlays=None, since=None, run_id=None)` is the read-snapshot
projection API. Its production counts argument must come from WP1
`query_counts`, not an independently computed counter. PR19 `catalog_links`
validates explicit agency/request pairs, including cross-agency originals.
PR19 is a dependency, not merged, cherry-picked or copied by this WP. The
unmodified PR19 focused suite is run through the dependency overlay alongside
WP5 tests. Production packaging must include both reviewed dependencies.

## Data and safety contract

Every canonical unique original gets one card, even with no metadata or blocked
extraction. Duplicate deliveries remain distinct occurrences on that card.
Agency/request joins are many-to-many; typed joins require an existing agency,
request-agency match and original hash plus locator evidence. Filename hints
remain hinted and cannot populate typed agency attribution. Unverified prior
digest labels stay declared labels, separate from canonical stage status.

Draft metadata uses a strict field allowlist, ISO date range validation,
source-hash locators and explicit unknown reasons. Scores are bounded integers
with reasons. Draft summaries, dates, types and titles require evidence. The
library does not infer facts from filenames or invoke a model. Low-value items
remain proposed, even though a later policy-controlled closure adapter may
apply a separately authorized narrow closure. No original is removed.

Counts are embedded byte-for-byte (HTML escaped) from the same WP1 result and
saved as counts.json. Stage counts reconcile against the projected state rows.
Unknown, stale or missing slots fail the whole export, not silently reduce its
denominator. Cards, open requests with determination dates, blocked owners and
proposal states are displayed. All text is escaped; there are no scripts,
remote assets, forms, source-file links or portal links. A restrictive CSP is
included. This is a private export, not a sanitized public records library.

Bounds: 10,000 originals, 100,000 rows for each ancillary metadata table,
70,000 stage slots and 64 MiB per exported artifact. Exceeding a bound fails
with a reason; the caller must queue a bounded redesign rather than truncate.
No raw unit text, email body, opaque original provenance or storage path is
selected into the board.

## Tests without copying candidate dependencies

Set `RECORDS_TEST_DEPENDENCIES` to a JSON array containing the local WP1 and
PR19 source roots. The synthetic suite adds only their records package paths
at test time. Run `python3 -B -m unittest tests.records.test_ledger_catalog -v`.
The integration class explicitly skips if either dependency is absent; report
that honestly. The host acceptance run must have zero dependency skips.
Run PR19's original `test_catalog_links.py` from its source as well.

## Remaining WP5 acceptance

- Independent review, public/private-source scans, parent integration and CI.
- Owner decision on PR19 integration and owner merge of work-package PRs.
- Request-table import from legacy request records; no guessed requests here.
- Historical queue-age/weekly-throughput metrics; present-run inventory metrics
  and pre-generated agency/type/year filters are implemented.
- Deployment behind owner-only Cloudflare Access with static assets and detail
  routes tested. No deployment or authentication claim is made by this export.
- Bounded representative real sample after parent accepts the candidate API;
  full-corpus processing remains deferred by the infrastructure-first request.

The consistent source capture is bounded to 8 GiB and a 30-second SQLite backup
deadline. Larger/slower databases fail explicitly instead of producing partial
cards. Repeated evidence joins for the same original/agency/request keep all
evidence rows while PR19 validates the unique context pair once. Opaque extra
join-evidence fields are not exported. Scope-excluded originals cannot acquire
typed joins, including agency-only joins.


## Transactional private snapshot selection (schema 2)

The candidate build may omit run_id and stays exportable, but cannot be accepted.
For acceptance, build with a canonical run ID whose row has completed/succeeded/
success status, a timezone-bearing ended_at, engine version and config SHA-256.
The run row is included in the snapshot identity. No run is fabricated or updated
by WP5. Image identity remains whatever the run attests, not independently
verified by the board.

Use ledger_catalog_acceptance.accept_snapshot(database, private_scratch,
private_output, snapshot_id, run_id=..., expected_catalog_sha256=...,
expected_manifest_sha256=..., expected_current_snapshot_id=None). The two
expected hashes bind exact catalog.json and artifact-hashes.json bytes.
All artifacts, including filtered pages, are rebuilt deterministically and
compared byte-for-byte. Schema, private paths, counts, joins and run binding are
checked; then a new consistent ledger capture must reproduce the whole catalog.
The capture is point-in-time, not a promise that future ledger writes stop.

An owner-only accepted-snapshots.sqlite stores the acceptance receipt, history
and single pointer in one BEGIN IMMEDIATE/FULL-synchronous transaction, under
the same export lock. Pass the prior snapshot ID to replace an existing pointer.
A replay of the current snapshot returns the original receipt without another
event. A post-commit response or directory-sync failure recovers by replay and
resync; a stale or superseded request cannot silently reactivate an old pointer.
read_accepted(private_output) revalidates the saved receipt and exact artifacts.
Acceptance selects the private board only. Candidate metadata remains candidate,
canonical stage status does not change, and publication approval stays false.

## Filters and metrics

No JavaScript or server is required: index.html links only to content-hash-named
local HTML pages for agency, type and starting-year filters. Unknown is a
first-class choice. Every page retains the complete canonical count payload and
labels its visible-card count separately. Metrics show unknown metadata, proposed
low-value items, catalog/review stage-done counts, blocked originals, open requests
and the explicitly selected arrival window. Weekly throughput stays null with a
reason until a historical accepted-run interval is selected. At most 200 filter
pages and 64 MiB total payload are allowed; the library fails rather than truncates.

## Composed package compatibility

The dependency_contract API checks the packaged WP1 query_counts surface and
PR19 schema-1 registry validator, reporting PR19 source SHA-256. A synthetic test
composes only generic engine source into an owner-private temporary package and
imports it with Python isolated mode, without test overlay variables or host path
discovery. The copied PR19 module must match the unchanged dependency bytes.
This is a compatibility test, not a wheel release, dependency merge or install.
