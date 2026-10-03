# Offline mailbox receipt delta (M1 increment)

This is a single-receipt, offline importer into an **existing** intake v3.2 SQLite output. It does not scan a mailbox or source tree, download, extract, catalog, send, publish, or schedule anything. The exporter remains the source of the receipt. The importer requires a complete JSON receipt with `account_id`, `folder`, `uidvalidity`, `uid`, `bytes`, `original_eml`, and `attachments`. `original_eml` needs `path`, `bytes`, and lowercase `sha256`; each attachment needs those fields plus a unique `part`. Attachment `original_filename` is validated against the selected MIME part's `get_filename()` (and may be null for an unnamed part); `filename` must exactly equal `safe_filename(original_filename)`. `content_type` may be null or absent, but when supplied it must match the MIME part. Headers and message narrative are ignored. All test content is synthetic.

Replay rehashes every existing canonical blob and rejects a missing, corrupt, or wrong-sized blob before returning a no-op result. Source files are also rechecked. A rejected replay leaves the active run unchanged. The focused synthetic suite and independent source review are recorded with the PR. This remains an offline adapter, not authorization to change scheduling, permissions, or live services.

## Proposed manual invocation after the hold is cleared

```sh
python3 -B -m campaign_tool.records.intake.mail_delta \
  --receipt "$ONE_PRIVATE_RECEIPT_JSON" \
  --mail-root "$TRUSTED_PRIVATE_MAIL_EXPORT_ROOT" \
  --output "$EXISTING_PRIVATE_INTAKE_OUTPUT"
```

The receipt is never allowed to name a source outside the explicit mail root. Absolute paths must remain within it; relative paths are interpreted beneath it. `..`, symlinked components, nonregular files, changed files, wrong sizes/hashes, and oversized inputs are rejected. The importer holds the canonical `writer.lock` while staging and committing. The existing output, database, and blob directory must be owner-only and already present. It creates owner-only temporary blobs, seals exact SHA-256 blobs, and commits the ledger update as one transaction. A crash may leave an unreferenced immutable blob or a staging `.pending-*` file, but not a partially promoted inventory run. This module does not sweep crash residue at startup; durable-job integration must reconcile temporary files against hashes and the ledger before any cleanup. On an ordinary rejected import, newly created blobs are identified by inode and removed under the writer lock only when no preservation row references them; preexisting blobs are never removed. It emits only status, counts, run ID, or fixed error codes, never headers, message text, or source paths.

One run copies every `seen` row from a previous **complete** active run and appends the receipt's message and attachment occurrences. A partial or running active run cannot be promoted. Path-only and hash-based scope exclusions both block import; relative exclusion paths are compared beneath the explicit mail root even when the receipt carries absolute source paths. The previous run is untouched. Occurrence identity is account/folder/UIDVALIDITY/UID/part; a same-identity/different-hash or changed source path fails closed. Already known attachment hashes get new occurrence provenance without duplicate `docs` rows or changes to stage, digest, review status, or units. Attachment child edges use the intake MIME locator, while occurrence locators retain the exact exporter `part` and source path. The receipt top-level `bytes` must equal `original_eml.bytes`. Every attachable MIME part must be listed in a complete receipt, and body-only text parts cannot be listed as attachments. Ambiguous unnamed inline structured text (including `text/calendar` and `text/csv`) fails closed. An extra unnamed inline `text/plain` or `text/html` child under `multipart/mixed` also fails closed; body alternatives under `multipart/alternative` remain accepted. A JSON integer exporter `part` is a zero-based index over the full `enumerate(msg.walk())` order, including multipart container nodes; it is not a leaf ordinal. The intake MIME locator (`1.1.2`, for example) is retained separately. String parts may name the full MIME locator (`1.2`) or a direct-root child number (`2`). A hash match at the wrong index or locator is rejected. Decoded MIME payload bytes and content type must match the selected MIME part. Receipt `original_filename` must equal the MIME part `get_filename()`. Receipt `filename` must equal `safe_filename(original_filename)`: split the basename on `/` and `\`, replace each character outside `[A-Za-z0-9._-]` with `_`, strip leading/trailing `.`, truncate to 100 characters, then use `attachment.bin` if empty. Document format is derived from the MIME filename, not an unverified receipt label. Unsupported nested RFC822 parts and ambiguous relationships fail closed. A replay that changes nothing should not create another run or occurrence. Blob-sealing conflicts are reported as fixed rejection codes without changing the ledger.

Per-receipt limits: 1 MiB JSON, 64 MiB EML, 128 MiB per attachment, 512 MiB total source bytes, 100 attachments, and 1,000 MIME parts. More complex or larger receipts require a separately reviewed change. No general report files are regenerated here; consumers should read the active `meta.inventory_run` and its `seen` rows. Any later extraction or report operation remains separate and must be reviewed for its own output/privacy behavior. No parser work here constitutes independent record or legal review.

Synthetic regression command:

```sh
python3 -B -m unittest tests.records.test_mail_delta -v
```

Before any private run: back up the canonical database and blobs, verify source/root/output permissions and free space, run the synthetic suite, have an independent review of the exact diff and receipt schema, then use one receipt at a time and reconcile active/previous counts. No timer, exporter retirement, network, host package, outbound action, or publication is included.

## Bounded multipart/related body roots

Related body selection follows [RFC 2387 sections 3.1-3.2](https://www.rfc-editor.org/rfc/rfc2387):
an explicit start must match exactly one immediate child's Content-ID; with no
start, the first child is the root. IDs are case-sensitive, angle-bracketed,
and compared without URI decoding or descendant searches. Duplicate child IDs,
multiple Content-ID headers, malformed IDs/start values, defective related
containers and an unmatched start fail closed with fixed codes. This bounded
adapter accepts simple IDs without embedded whitespace; more complex RFC822
ID syntax needs separate review. A supplied type must match the selected
root's media type (case-insensitively). Omitted type remains tolerated for
compatibility with exporters, although RFC 2387 requires it.

Only the selected root can inherit body permission. That permission follows
the entire ancestry: alternatives may share it, mixed permits only its first
child, and related permits only its selected child. Wrapping a non-body text
part in another related/alternative container does not make it a body.
Unnamed inline text outside that path still fails closed, as does structured
inline text such as CSV/calendar. Named or explicitly attached text remains an
attachment, even at the selected root; related handling is not a blanket
Content-Disposition exemption.

Inline named images and every other attachable leaf must still appear in the
receipt and bind by exact walk index or MIME locator, decoded byte hash/size,
original/export filename and content type. Neither traversal order nor edge
and occurrence locator formats change. Missing attachments, wrong indices,
and same bytes at different locators retain the existing safeguards and replay
contract. The existing part budget also bounds pending children.

Nested unnamed message/rfc822 remains unsupported_rfc822_part; this repair
does not add forwarded-message support. All new tests generate synthetic MIME
and temporary ledgers, never read a mailbox, and never retry private intake.

    python3 -B -m unittest tests.records.test_mail_delta tests.records.test_mail_related -v

## Approved fail-closed header and container follow-up

Before any MIME node supplies media-type classification or body permission,
its Content-Type must be absent or exactly one defect-free parsed header.
Duplicate headers (even equal values), invalid media types and defective
parameters reject with invalid_mime_content_type. The selected related root is
validated before comparing an explicit related type. An absent Content-Type
continues the existing MIME default (normally text/plain); absence is not
treated as a malformed supplied value.

A named or explicitly attached multipart entity rejects with
unsupported_attached_multipart_part before traversal. This applies to selected
roots, outer containers and non-root resources, including names from
Content-Type. The importer cannot bind that container's exact original bytes
as an attachment, so it never silently descends and loses the original's
identity. Supporting such containers requires separately reviewed exact-byte
binding semantics, not a body exemption.

Multipart Content-Disposition must be absent or a single defect-free header
(invalid_multipart_disposition otherwise). An unnamed inline container remains
a valid body container. Declared multipart bodies with broken structure reject
with invalid_mime_container. Named/attached nonmultipart leaves retain exact
receipt binding. Ordinary related body controls, part limits and unsupported
nested message/rfc822 behavior remain in place.

All follow-up rejection regressions use generated MIME and temporary ledgers
and assert no run promotion, ledger mutation or new canonical blobs. Earlier
failed test/CI results and independent review evidence are retained; passing
synthetic tests do not substitute for rechecking the frozen repaired candidate.

## Approved leaf disposition follow-up

Every nonmultipart leaf must also have an absent or single defect-free
Content-Disposition header before payload/body/attachment classification.
Duplicate headers (including identical inline values) and malformed supplied
values reject with invalid_leaf_disposition, even when one interpretation would
be a selected body root. Multipart checks and their existing codes are unchanged.
Valid single inline bodies and explicit unnamed leaf attachments remain supported.

The public function signatures, candidate and receipt fields, exact MIME
locators, walk indices and identity scheme do not change. Consumers still receive
the existing Rejected failure class, with invalid_leaf_disposition as an
additional fixed code. Any consumer that enumerates permitted failure codes must
include it; this change does not modify a consumer allowlist or add RFC822 support.
