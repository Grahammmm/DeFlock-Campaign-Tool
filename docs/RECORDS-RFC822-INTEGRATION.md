# RFC822 intake integration contract (dependent, not enabled)

This slice depends on the frozen RFC822 wire helper in PR54. It adds only
`rfc822_inventory.py`, `rfc822_adapter.py`, synthetic integration tests and this
contract. It does not modify `wire_rfc822.py`, `eml_export.py`, `mail_delta.py`,
`imap_intake.py`, their existing tests, safety projections, or live services.
It is a pure preparation adapter, not installed operational intake. PR54 stays
frozen while its separate independent review proceeds.

## API and owner boundary

```python
from campaign_tool.records.intake.rfc822_adapter import (
    prepare_rfc822_intake, classify_scalar_parts, bind_rfc822_receipts,
)
from campaign_tool.records.intake.rfc822_inventory import RFC822Limits

plan = prepare_rfc822_intake(raw_bytes, limits=RFC822Limits())
# Existing reviewed body/related-root classifier supplies EVERY scalar role.
roles = {part.part: classify_with_existing_policy(part, plan.inventory)
         for part in plan.inventory.scalar_parts}
plan = classify_scalar_parts(plan, roles)  # values: "body" or "attachment"
metadata = list(plan.receipts)
bound = bind_rfc822_receipts(plan, metadata)
```

`inventory_rfc822(raw_bytes, *, limits=None)` returns immutable message sources,
all scoped MIME parts, RFC822 captures and shared usage. Parts expose exact
header/body offsets, content type, CTE, Content-ID, disposition, original
filename, headers and local multipart parent locators. This preserves the
context needed by the existing selected-body/related-root policy rather than
inventing a competing heuristic. The adapter does not decide that policy.

`prepare_rfc822_intake` returns an immutable plan containing every RFC822
wrapper, named or unnamed. It is explicitly incomplete until all scalar leaves
in all contained messages receive roles. `classify_scalar_parts` requires an
exhaustive mapping, retains every RFC822 capture, decodes selected scalar
attachments strictly and charges the same budgets. Reclassification rebuilds
from the original inventory; it does not append duplicates or reset budgets.
`bind_rfc822_receipts` requires a completed plan and binds every capture exactly
once, in canonical plan order regardless of supplied order. Missing, extra,
duplicate, forged size/hash/filename/type/format, and wrong-parent records fail.

Only selected scalar attachments are decoded. Body roles are explicit
owner-policy decisions; this adapter does not certify their correctness.
Captured MIME inventory is not archive-member extraction, substantive analysis,
independent review or authorization to promote the real ledger.

## Exact bytes and immediate parent provenance

Each contained EML is the exact identity-decoded wire range, excluding exactly
one delimiter framing newline. No `EmailMessage.as_bytes()` is used. Each
message is independently mapped against stdlib only after bounded outer wire
validation; its own RFC822 children remain opaque in that comparison pass.
The inventory then deliberately enters each captured EML under the SAME budget.
Malformed contained MIME fails the entire plan, rather than yielding a hidden
partial success. Base64/quoted-printable RFC822 wrappers remain unsupported.

An occurrence key is `rfc822:` followed by the per-message MIME locators joined
by `/`; scalar part keys use `mime:`. These are source identifiers, NEVER
filesystem paths. The outer original is `0`. `mime_chain` keeps message-context
transitions explicit; `mime` is the local wrapper/leaf locator. `parent_part`
identifies the immediate enclosing original/captured EML occurrence, not always
the outer original. `parent_sha256` binds that enclosing message's exact bytes.
`wire_start`/`wire_end` locate the source body range within that parent EML.
Nested multipart relationships remain separately available in `WirePart`.

Equal hashes retain distinct occurrence keys and parent edges. Duplicate
messages consume budget for EACH occurrence. Writers may deduplicate immutable
blob storage by hash but must never deduplicate receipt/edge occurrences.
Unnamed RFC822 captures retain `original_filename: null`, a safe fallback
`filename: attachment.bin`, and explicit `kind: eml`/`format: eml`. That explicit
format, not filename suffix inference, tells the consumer this is an EML.

The schema is `rfc822-wire-receipt-v1`; `as_manifest()` includes the outer byte
identity, capture metadata, exhaustive scalar roles, completeness and budget
usage. It contains no mailbox/account configuration. It is NOT a drop-in legacy
mail receipt. The owning exporter must add verified private paths and outer
account/folder/UID identities through a versioned integration.

Binding checks metadata/membership, not files. An optional `path` is passed
through as writer data and is NOT trusted, opened, normalized or certified here.
The importer must keep its existing owner-only/root-containment/no-symlink/
regular-file/change-detection checks and verify actual staged bytes before any
transaction. It must rederive the plan from the preserved outer original and
bind that plan rather than trusting a supplied manifest or constructed plan.

## Shared ceilings and strict scalar decoding

- Per-message source: 64 MiB; all source bytes are immutable nonempty `bytes`.
- Cumulative bytes: 512 MiB, outer original plus EVERY captured EML occurrence plus selected decoded scalar attachment payloads.
- Global MIME parts: 1,000, including every contained-message root.
- Global depth: 32, counting multipart descendants AND RFC822 message-root transitions.
- Captures: 100 across RFC822 and selected scalar attachment occurrences.
- All limits may only be lowered to positive integers; booleans are rejected.
- Header/boundary subset and bounds are inherited unchanged from the frozen wire helper.
- Scalar identity CTEs preserve body bytes. Base64 requires strict validation and canonical padding/pad bits. Quoted-printable accepts explicit hex escapes and LF/CRLF soft breaks, with 76-byte encoded lines and no bare CR, non-ASCII literals or trailing transport whitespace.

Byte accounting is occurrence-based, not unique-hash based. Scalar decoding
makes bounded temporary allocations before the final cumulative admission
check; this is not streaming or a strict process-memory cap. Header/stdlib
views and capture slices add bounded memory overhead. Body roles do not create
captured payloads. Later archive/OCR/extraction work needs its own run budget;
this adapter does not replace mailbox or worker admission limits.

## Exact integration paths, reserved to the coordinating owner

- `campaign_tool/records/intake/eml_export.py`: consume this inventory instead of recursively walking/reconstructing RFC822 nodes; write every capture after the full plan succeeds; use explicit EML kind in attachment forms.
- `campaign_tool/records/intake/mail_delta.py`: recognize the versioned receipt namespace, securely stage/bind ALL required captures and store immediate-parent occurrence edges in one transactional promotion; retain existing scalar/body ambiguity checks.
- `campaign_tool/records/intake/imap_intake.py`: add typed literal safe rejection-code projection for inventory/adapter errors; keep BODY.PEEK, folder/UID, replay, fetch cap and checkpoint semantics unchanged.

Those modules remain owned by the PR50/IMAP author until coordinated handoff.
Safety consumers must project only exact error types and literal codes from
`RFC822_FAILURE_CODES`, never arbitrary exception text or guessed prefixes.
No overlapping edits are included in this branch. Final composed testing must
cover exporter staging failures, importer rollback/replay, every nested parent
edge, same-byte distinct occurrences, global budgets and checkpoint safety.

## Synthetic checks

```sh
python3 -B -m unittest tests.records.test_wire_rfc822 tests.records.test_rfc822_integration -v
python3 -B -m unittest discover -v
python3 -B tools/check_public_tree.py --patterns-only
node scripts/scan-secrets.mjs
```

All test messages are generated synthetic bytes. Tests exercise named/unnamed
nested captures, original folding and line endings, no reserialization,
explicit scalar roles, strict decoders, shared limits, repeated occurrences,
forged/missing/unlisted receipts and immediate-parent relationships. Independent
review and composed integration remain required before operational readiness.
