# WP6 canonical detector persistence

This additive adapter stores small supplied-evidence evaluations privately. It does
not scan a corpus, claim a stage, issue accepted review receipts, promote detect,
close low-value records, or change acceptance or publication state.

## Stable API

```python
from campaign_tool.records.detectors.persistence import persist_evaluation
receipt = persist_evaluation(
    connection, run_id=run_id, detector=detector_id,
    units=units, joins=joins, rules=rules, config=config,
)
```

`connection` is an existing private WP1 SQLite connection, outside a transaction.
The caller owns database path validation and private filesystem permissions. The
adapter enables foreign keys and owns one `BEGIN IMMEDIATE` transaction. It does
not open files or contact services, but is intentionally not a pure function.
Caller transactions are rejected rather than silently committed.

One canonical `runs` ID represents one detector evaluation. The caller must create
an active run of kind `detector`, `detector-runner`, or `records-runner`, with a
nonblank engine version, timezone-bearing start time and `config_sha256` equal to
`core.digest(config)`. For seven detectors, allocate seven evaluation run IDs.
A parent runner's batch ID is not a substitute for these distinct evaluation IDs.
The adapter does not create or finish runs. A completed run may replay an already
stored exact evaluation but may not start a new one. Run configuration and engine
provenance are bound independently of mutable status/end time/summary.

The pure evaluator computes the result internally; callers cannot submit an
arbitrary manifest, hit list or validation callback. Persistence requires an
explicit rule-pack version. Inputs use the existing pure detector schema and
bounds, plus a maximum 10,000 hits and 16 MiB serialized result per evaluation.

## Canonical rows and additive extensions

Canonical `detector_runs` stores the detector version, rules version, classified
counts and exact manifest hash. Canonical `detector_hits` stores one first-seen
semantic hit per existing dedupe key, including its first run and exact evidence.
The first-seen row is never overwritten merely because another run sees the hit.
Do not count current-run hits by filtering this first-seen `run_id` column.

New `wp6_manifests` binds the adapter version, full classified manifest, canonical
input/component hashes, full result hash and run provenance. `wp6_run_hits` links
each run to every hit it observed and retains that run's full hit JSON and hash.
`wp6_hit_identities` checks shared semantic identity and protects the first-seen
canonical snapshot through comparison on reuse/replay. `wp6_input_originals` binds
valid supplied original hashes to existing canonical original metadata. A valid
but unknown original hash fails closed; a missing hash can remain a blocked pure
input outcome without an invented original association.

A semantic hit can have new print locations or be included in a relabeled rule
pack. Those differences stay in per-run evidence, not a destructive global-row
update. Actual applicable rule content and semantic observations remain bound by
`wp6-pure-detectors-v2` keys. Full result and input hashes distinguish such runs.

Hashes of structured inputs are SHA-256 over `core.canonical` JSON, not claims
about arbitrary source JSON byte serialization. Original-byte identities remain
separate `original_sha256` references. This adapter does not re-read original
bytes, authenticate a parser, validate normalized units against a parser's source
payload, or certify supplied agency/rule context. Those are configured runner and
stage-validator responsibilities; supplied-evidence persistence is not acceptance.

## Atomicity, replay and limits

Canonical rows, manifest, identity records, run-hit links and original bindings
commit together. A write failure rolls back the evaluation. Exact replay validates
manifest, counts, canonical hit snapshots, full per-run hits and source bindings;
it performs no replacement. Changed input under the same run ID is rejected.
Use a new run ID for a correction. No silent adoption of old unmanaged canonical
runs/hits, partial migrations or missing extension guards is permitted.

The new extension tables reject UPDATE, DELETE and replacement INSERT even with
recursive triggers disabled. No existing WP1 table definitions, base migration,
stage implementation or canonical-table triggers are modified. Consequently,
external mutation of canonical detector rows is detected on adapter replay/reuse,
not prohibited globally. This is not a defense against a database administrator
who deliberately removes guards or rewrites every hash and record.

Queries and replay checks are limited to the supplied bounded evaluation and
indexed run/hit/original keys, not scans of all prior payloads. Large-corpus
throughput, scheduling, current timezone/legal applicability and independent
acceptance remain out of scope. No production validator is installed by this code.

## Test composition

Synthetic tests import the actual WP1 `ledger.migrations.v001.SQL`. Before WP1 is
merged into the detector checkout, the test launcher explicitly composes the WP1
records package path; tests do not silently skip when that dependency is absent.
Run `tests.records.test_detector_persistence.PersistenceTests` together with the
two existing detector test classes. All databases and records are synthetic.


## Independent-review hardening

Existing extension migrations must match the complete stored SQLite SQL for every
versioned table and trigger. Matching object names or a metadata checksum alone
is insufficient. Same-named no-op/mis-targeted triggers, unversioned table changes
and missing definitions fail closed. Unexpected SQL formatting also requires an
explicit migration rather than silent adoption. The base WP1 schema is untouched.

A run's timezone-bearing start must not exceed the installed process UTC clock.
The adapter exposes no caller clock parameter; neither config.as_of nor other
record fields authorize a future start. Equal instants with different UTC offsets
are handled consistently. Clock trust is an execution-environment assumption, not
a claim of externally authenticated time. No clock value enters detector hashes.

Before JSON serialization or copying, the adapter checks the 5,000-unit,
10,000-join and 256-rule cardinalities and native JSON row/object shapes. A bounded
walk additionally limits inputs to 100,000 structural nodes, depth 32, 8 MiB total
string characters and 4096-bit integers, rejecting cycles, unsupported values and
nonfinite numbers. The existing 8 MiB serialized-byte check remains authoritative
for Unicode/escaping expansion. This bounds adapter processing, not allocations
already performed by the caller to construct its inputs. Invalid input causes no
database writes. Optional WP1 test discovery and the pinned CI overlay are unchanged.
