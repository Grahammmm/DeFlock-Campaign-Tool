# Stable WP3 boundary for combined runner acceptance

Interface version: `portal-runner-v1`. This document fixes the callable and result
shape for the current candidate. Any incompatible change requires a new version.
The parent runner remains responsible for runtime identity, scheduling, leases,
trusted stage validation and activation. No WP2 files or WP1 implementation are
copied or changed here.

## Construction

```python
from campaign_tool.records.portal import Queue
from campaign_tool.records.portal.ledger_adapter import WP1LedgerAdapter
from campaign_tool.records.portal.runner_bridge import PortalRunnerBridge

queue = Queue(private_queue_root)
ledger = WP1LedgerAdapter(
    queue=queue, database=canonical_database, cas_root=canonical_cas_root,
)
bridge = PortalRunnerBridge(
    queue=queue, ledger=ledger, approval_loader=trusted_approval_loader,
    egress=independently_permitted_egress_checker, transport=configured_transport,
)
```

Paths and callables come from trusted private configuration. The canonical
database must already be initialized by the installed WP1 dependency. Missing or
incompatible dependency, schema or storage binding fails closed. Source notice
hashes refer to preserved messages; resolving a notice to a canonical mail
occurrence remains an explicit upstream adapter requirement.

## Stable calls

- `bridge.run(*, apply=False, limits=None) -> dict`: dry inventory by default;
  explicit apply retrieves permitted items and drains the ledger outbox.
- `bridge.drain_outbox(*, apply=False, limit=100) -> dict`: operates only on
  already-preserved bytes. It never invokes approval/egress/transport callbacks,
  DNS or HTTP. Explicit apply retries canonical enrollment; valid limit is 1-1000.
- `ledger.record_original(receipt, object_path) -> dict`: called under the queue
  writer lock; verifies bytes and receipt/attempt bindings and commits canonical
  enrollment. Return contains subject hash, delivery run ID, zero stage
  promotions, `preserve: pending` and `catalog: pending`.
- `ledger.pending_card_inputs(limit=100) -> list[dict]`: each row has `sha256`,
  `bytes`, `storage_path`, `status`. These are canonical pending catalog inputs,
  not completed catalog cards. The result is bounded and may include inputs from
  earlier runs, so the caller must deduplicate work by original hash and stage.
- `bridge.validate_and_promote(...)` and `ledger.validate_and_promote(...)`
  explicitly refuse with `trusted_portal_stage_adapter_unconfigured`.

Every bridge result has exactly these top-level fields:

```json
{
  "interface_version": "portal-runner-v1",
  "portal": {},
  "pending_card_inputs": [],
  "stage_promotions": 0,
  "canonical_integration_verified": false,
  "pipeline_complete": false
}
```

`portal` is an operation-specific structured report. Local outbox results include
`mode: local_outbox`, `ledger_delivered` and `ledger_pending`; a failed delivery
adds the fixed category `ledger_error: ledger_delivery_pending`. A delivery
failure keeps the receipt pending for retry. Delivered counts are derived from
acknowledged outbox transitions under the writer lock, including earlier successful
deliveries before a later failure. A canonical commit awaiting outbox acknowledgment
is still pending delivery and is safely retried using its unchanged receipt ID. Status/report fields never include
signed URLs. Do not log arbitrary raised exception narratives.

## Combined runner ordering

1. Acquire the runner's global lock, then use the portal queue lock internally.
   Do not acquire these in reverse order in another process.
2. Inventory notice links only after the existing mail adapter verifies their
   original message bytes and request/item relationship.
3. Drain existing outbox work locally; retrieval may run separately when its
   independent approval and egress gates permit it.
4. Enroll permitted new originals and typed portal occurrences through the adapter.
5. Queue canonical work by `(original_sha256, stage)`, beginning with the trusted
   preservation validator. Seven newly created stage slots remain pending.
6. Advance extraction/catalog and subsequent stages only after their actual WP1
   gates accept receipts. A portal delivery acknowledgment is not a stage receipt.
7. Derive progress from the canonical ledger and pending outbox. A successful
   component call cannot set whole-pipeline completion or activation readiness.

The canonical delivery run is `portal-delivery:<receipt-id>`, recording adapter
identity and frozen provenance. It is not the parent runner's runtime-attested
run. The parent may retain this ID as a child operation; avoid equating the two.
Unknown agency/title/mail occurrence values remain null. Existing immutable
originals with unresolved or different CAS paths cause an explicit integration
gap; they are never silently rewritten.

## Reusable offline acceptance

```sh
RECORDS_WP1_SOURCE_ROOT=/trusted/wp1-candidate \
  python3 -B -m unittest tests.test_records_portal_runner_contract -v
```

This loads the actual WP1 dependency from an explicitly trusted source overlay,
creates temporary synthetic queue/CAS/ledger roots, and exercises notice through
portal bytes, canonical enrollment and pending catalog inputs. It verifies seven
pending stages, duplicate-safe replay, offline outbox recovery after ledger commit
but before acknowledgment, dry-run behavior and refusal to promote stages. Socket
creation and DNS are forbidden during the local drain test. The complete portal
suite separately tests HTTPS using protocol fixtures; it makes no live requests.

The source overlay is test-only. Production must import the pinned installed
engine. Parent review, exact runtime/lease integration, trusted preservation-stage
validation, provider-specific adapters and permitted network acceptance remain
required. This document does not authorize network access, scheduling or deployment.

The dedicated integration CI job checks out an exact public ledger-candidate commit as a test dependency. It does not silently skip canonical integration when the ledger is not yet merged into main. This is a test dependency, not an installed deployment; production still requires a reviewed pinned combined release. Any ledger API change requires an explicit dependency-pin update and a new integration run.
