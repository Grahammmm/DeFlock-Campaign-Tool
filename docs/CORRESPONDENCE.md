# Correspondence projection and reusable skill

The correspondence slice provides a read-only private report over the canonical
records ledger and a source-only skill at
[`skills/flock-correspondence/SKILL.md`](../skills/flock-correspondence/SKILL.md).
It organizes existing agency requests, linked mail and attachment occurrences,
delivery duplicates, evidence gaps, and optional reviewer proposals for outstanding
items and follow-up dates. It introduces no schema migration, mailbox service,
second request registry, dashboard, or scheduler.

## Run the offline report

```sh
python3 -B -m campaign_tool.records.correspondence \
  --ledger "$PRIVATE_LEDGER" \
  --as-of 2026-10-13T12:00:00+00:00
```

`--ledger` names an existing canonical database; `--as-of` requires a
timezone-aware ISO timestamp. The JSON report goes to stdout and must be treated
as private. Paths, packet files, reports, and originals belong outside this
repository in the owner's restricted evidence workspace. The report's as-of time
selects due reminder candidates from the current ledger snapshot. It does not
reconstruct historical state or establish the scope or freshness of the
underlying mailbox export. `snapshot_sha256` binds the selected canonical rows
read in one transaction, rather than all original bytes or the database file.

An optional `--review "$PRIVATE_REVIEW_PACKET"` accepts a transient
`correspondence-review-v1` packet bound to the report's `snapshot_sha256`. See the
[workflow and packet example](../skills/flock-correspondence/references/workflow.md)
for the exact fields, evidence requirements, and date interpretation. Review
assertions remain proposals and do not modify canonical request status or certify
production completeness. The skill can be read directly from this checkout; this
change does not install it into an agent's runtime or alter discovery settings.

## Readable version

Render an already-produced private JSON report without opening a ledger or mailbox:

```sh
python3 -B tools/correspondence_readable.py --report /absolute/private/report.json --output-dir /absolute/private/new-thread
```

Both paths must be absolute and normalized; the output directory must not exist,
and its parent must contain no symlinks. Input is bounded to 32 MiB.
The new directory is 0700 and `correspondence.md` is 0600. Keep it outside git.
The Markdown groups recorded request dates and deliveries by agency and request;
reply links remain unverified, gaps remain explicit, and follow-ups are proposals.
It makes no fulfillment or legal assessment and sends or publishes nothing.

## Existing interfaces and compatibility

The projection uses the existing authority rather than copying its schema:

| Source interface | Use and limitation |
| --- | --- |
| [`ledger/store.py`](../campaign_tool/records/ledger/store.py), `ledger(..., readonly=True)` | Opens the canonical ledger with read-only/query-only settings, validates base migration identity, and enforces owner-only path/file requirements. This report does not initialize or upgrade a source. |
| [`ledger/migrations/v001.py`](../campaign_tool/records/ledger/migrations/v001.py), `agencies`, `requests` | Existing agency/request IDs, recorded status, external reference, and submitted/determination/production dates. There is no durable per-item obligation table here. |
| Same schema, `originals`, `occurrences` | Separate byte identity from each receipt context; attachment/archive lineage is `parent_occurrence_id`. Equal hashes do not erase occurrences. |
| Same schema, `joins` | Typed, hinted, or blocked agency/request links. This report checks evidence shape and consistent request/agency identity, but labels accepted links `recorded_typed_join_not_reverified`. It does not re-run catalog source acceptance. A header thread does not replace a join. |
| Same schema, `mail_messages` | Mail occurrence identity is `(account, folder, uidvalidity, uid)`, with original hash, headers, received time, and account provenance status. Message-ID alone is not that identity. |
| Same schema, `portal_items` | Lists unresolved retrievals. Its `request_id` is an external portal reference without a foreign key to canonical `requests.id`; it must not be interpreted as an accepted request join. |
| [`runner/canonical_mail.py`](../campaign_tool/records/runner/canonical_mail.py) | Produces bound header projections such as `verified-eml-headers-v1`, records missing/ambiguous Message-ID and exporter-declared received time, and preserves the account's provenance status. Parsing headers does not attest the provider. |
| [`ledger_catalog.py`](../campaign_tool/records/ledger_catalog.py) | Existing read projection over canonical requests and typed joins. Correspondence adds a focused report without replacing catalog or stage authority. |
| [`workers/schema/d1.sql`](../workers/schema/d1.sql) | A separate Worker schema with singular `request`, `correspondence`, and `receipt_occurrence` tables. Unsupported as input to this command; no implicit D1 adapter or registry merge exists. |
| [`intake/folder.py`](../campaign_tool/records/intake/folder.py) | Legacy filesystem intake has `docs` and `(oid, sha)` occurrences. Unsupported as direct correspondence input; captured legacy declarations are not canonical acceptance. |

The offline starter's document receipt ledger and operational pipeline stores
likewise are not interchangeable with the canonical schema. See
[canonical ledger](RECORDS-LEDGER.md) for its candidate-import and deployment gates.
A supported schema is not proof that all source mail or requests have been enrolled.

## Interpretation and non-goals

The report distinguishes a unique original from repeated deliveries and retains
source occurrence relationships. `Message-ID`, `In-Reply-To`, and `References`
provide header-declared thread candidates. They do not establish an authoritative
request link, sender identity, or substantive equivalence of changed bytes.
Missing, ambiguous, unsupported, or cross-request evidence stays visible for
review. No subject or sender heuristic automatically repairs a canonical join.

Request context must resolve to one request across the occurrence's original and
its ancestors. Conflicting same-byte delivery contexts remain gaps and cannot be
used as review evidence. Existing scope exclusions also suppress original and
descendant identities from evidence displays. Total ledger counts remain separate
from visible mail and excluded-occurrence counts.

Receipt dates and request dates retain their distinct meanings. This module adds
no conduct-date, retention-date, or legal-applicability fields. A contextual
analysis adapter must bind those separately sourced date roles to its own revision
inputs and keep missing applicability unresolved; receiving a record does not date
the conduct described in it.

Recorded statuses and dates are reproduced as ledger facts. A reviewer can propose
that an item is `outstanding`, `received`, `withheld`, `withdrawn`, or `unknown`,
with evidence, but neither the proposal nor successful validation updates the
request or proves complete production. No raw MIME/body parsing, attachment
extraction, substantive document review, legal comparison, or portal retrieval is
performed by this reporting command. Preserve the existing document owner's role.
The output's `outstanding_items` includes all submitted item dispositions; it
does not contain only unresolved items. `fulfillment` remains `not_assessed`.

Follow-up dates are operational proposals. The command does not calculate legal
deadlines, interpret jurisdiction packages, infer extensions, or declare violations.
Date-only and timezone-free values need review against the source and local
context before they can supply an executable time. Reminder proposals do not
create timers, jobs, notifications, or external messages. Existing outbox,
approval, runtime, and publication controls remain separate integration points.

The report and review packet are private analysis, even if they contain no message
body. They may expose agency/request identifiers, delivery dates, provenance, and
reviewer-entered descriptions. Public engineering updates must use synthetic
fixtures or sanitized implementation findings, never raw reports or corpus values.

## Migration and integration plan

No migration runs in this slice. Before a future integration, coordinate with the
current canonical-ledger and records-pipeline owners and verify the deployed
interfaces against their pinned source. Do not bootstrap a parallel registry to
compensate for missing enrollment or copy private production history into this
public repository.

1. **Reconcile current evidence.** With the records owner, account for existing
   requests, typed joins, mail projections, parent chains, portal gaps, and coverage
   cutoff. Preserve IDs and source provenance. Route incorrect or absent joins
   through the existing owner-controlled acceptance path. Re-run read-only reports
   against synthetic fixtures before any private rollout.
2. **Design durable obligations in the canonical ledger.** If explicitly requested,
   propose an additive, versioned migration in the same authority. Retain stable
   request/item IDs and append-only assertion history, exact evidence bindings,
   attribution, timestamps, and supersedes/retraction links. Distinguish an agency's
   promise from a reviewer's assessment. Do not backfill unknown dates, turn review
   packets into approvals, or overwrite historical assertions. Approval of this
   design and its acceptance rules precedes a real migration.
3. **Integrate one existing scheduler only when authorized.** Give it accepted
   obligations through a narrow adapter, with timezone-aware instants, stable
   idempotency identities, revised-date handling, and explicit canceled/completed
   states. Reconcile existing reminders before enrollment. The report itself
   remains pure and cannot create a second scheduler. Test duplicate triggers,
   stale snapshots, reschedules, clock boundaries, and failures synthetically.
4. **Keep outbound action separate.** An explicitly authorized future adapter may
   hand a reviewed draft to the existing approval/outbox interface. Preserve its
   identity, idempotency, fee, privacy, and ambiguous-delivery safeguards. A due
   reminder or packet's `reviewed_by` value is never sending authorization.
5. **Validate a bounded rollout.** Prove read-only behavior, canonical ID retention,
   request/evidence binding, unchanged original hashes, duplicate-delivery counts,
   timezone handling, and rollback/reconciliation behavior before owner adoption.
   Hold deployment, production scheduling, credentials, and dashboard work for
   their separately scoped approval and owners. No private-corpus import is part
   of the source patch.

## Validation scope

Use synthetic canonical ledgers, mail-header projections, request joins, duplicate
deliveries, and review packets. Exercise stale snapshot rejection, evidence from
another request, unsupported schema, attachment ancestry, ambiguous headers, and
date handling without real mailbox credentials or service calls. Tests establish
software behavior for those fixtures; they cannot establish actual production
completeness or independent factual/legal/privacy review. Record the tested code,
commands, results, and remaining gaps with the patch.

### Source-patch validation

Validated with Python 3.11.16 on macOS in an isolated test environment:

- `python -B -m unittest tests.records.test_correspondence -v`: **34 passed**.
  Includes actual canonical intake against an in-memory fake IMAP server,
  unchanged database bytes/data, repeated deliveries, stale packets,
  cross-request attachment conflicts, scope exclusions, and timezone boundaries.
- `python -B -m unittest tests.records.test_correspondence tests.records.test_ledger
  tests.records.test_imap_intake tests.records.test_mail_wire_intake -v`:
  **75 passed**, using a non-symlink temporary directory for canonical path checks.
- Full discovery: **2,021 tests**, with **58 failures, 237 errors, 47 skips**.
  The unchanged [baseline](https://github.com/Grahammmm/DeFlock-Campaign-Tool/commit/39b9242d7f19f3acbb87cae4e021c6be810eb836)
  ran **1,987 tests** under the same environment with the identical failure and
  error identities and skip count. The broad suite is not green on this host;
  these results are not a Linux runtime or production-readiness attestation.
- The skill-creator frontmatter validator passed. Independent synthetic source
  review verified the request-context, exclusion, and timestamp fixes. A separate
  skill execution produced the private status report and an unsent clarification
  draft, leaving unsupported item dispositions and follow-up dates unresolved.
- `node scripts/scan-secrets.mjs` passed on the staged source tree;
  `python -B tools/check_public_tree.py` reported zero findings across 541 source
  files. `detect-secrets scan --all-files --no-verify` over the five changed files
  reported zero findings. This patch contains authored source and synthetic tests,
  not exported private records.
