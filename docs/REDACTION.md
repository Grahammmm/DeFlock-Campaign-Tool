# Private redaction records

`tools/flock_redaction_record.py` is a read-only contract. It creates no proposal,
signature, derivative or approval. Machine eligibility is not semantic acceptance.
All exported findings still have `review.status: pending`.

## Citation and original identity

At citation time record the original's absolute `source_path` on the fact, in
FACTS.csv's extended header (`flock_facts.V3`: V2 plus `source_path`). Import
preserves this field; legacy V1/V2 remain readable and retain their old headers.
No location is inferred from the source hash or from a second store/index.
The existing facts, verification and comparison readers have no recorded original
location; catalog/extraction paths are separate contracts, not facts citations.

G's citation-path decision supersedes legacy export compatibility: a missing
location, missing/unreadable original or multiple distinct locations for a cited
source refuses the finding as `redaction:source_mismatch`, even without a record.
Locations must be absolute, normalized, inside the store and have no symlink
component (the same rules as tool outputs). No recorded hash substitutes for
hashing the actual current original bytes. Moving a store requires reciting its
new locations and comparing again; never silently remap paths.

## Records and permissions

`record_dir(store, slug, source_sha256)` is the sole path builder:
`<store>/agencies/<slug>/redaction/<source_sha256>/`. It contains
`PROPOSAL.json`, `SIGNOFF.json` and `ELIGIBILITY.json`; all files are 0600,
all directories inside the store 0700, outside the repository and link-free.
The three version-1 schemas reject additional keys and require all defined keys.

- Proposal: document id, original hash, whether redaction is required, proposed
  category/locator spans, not-detected note, scan-receipt hash and creation time.
  Each locator is a one-based line, zero-based byte offset within that line and
  positive byte length. No page, matched text or surrounding context is recorded.
- Sign-off: scope `flock-redaction`, document/source/proposal identities, trusted
  signer principal, timestamp and SSH-armored signature. The exact signed bytes
  are `approval.payload('flock-redaction', document_id, PROPOSAL.json UTF-8 text)`.
  Hash the exact proposal bytes, including their final newline if present.
- Eligibility: matching document/source/proposal and signer/time, derivative hash
  and absolute contained derivative path, and applied timestamp.

Only a human holding an authorized signing key can sign. Verification reuses
`campaign_tool.approval.verify` and its root-owned trust-file and binary checks;
there is no typed-name fallback. The workflow below records verified signatures.

## Reader results

`eligibility(store, slug, source_sha256, document_bytes_sha256)` returns a state
(`eligible`, `ineligible`, `no_record`) and exactly one reason:

| Reason | Meaning |
| --- | --- |
| `no_record` | No record directory; exporter adds a review-required hash-prefix note, not approval. |
| `not_required` | A valid proposal explicitly says no redaction required. |
| `unsigned` | Required sign-off is missing or malformed. |
| `signer_untrusted` | Shared verifier rejects, or principal differs from the sign-off. |
| `proposal_mismatch` | Unsafe/malformed records or identities do not agree. |
| `source_mismatch` | Original identity/current original bytes differ. |
| `derivative_missing` | Derivative or eligibility record is absent, or derivative path unsafe. |
| `derivative_mismatch` | Derivative bytes differ from their recorded hash. |
| `ok` | Current bytes, proposal, trusted signature and derivative agree. |

The exporter refuses an entry once if any cited document is ineligible, naming
only finding id and reason. Notes do not inflate its refused count. A valid
not-required proposal needs no signature. No-record notes use hash prefixes only.
Other publishing paths and record writers are separate staged work.

## Private text workflow

All commands below run from the engine checkout. STORE, DOCUMENT, RECEIPT and
signature/payload paths are absolute private paths, never paths in this repository.
SHA is the original's lowercase SHA-256; SLUG is its existing agency slug.
DOCUMENT is UTF-8 extracted text (not a PDF or a visual/page redaction).
Keep it in its own 0700 folder within STORE and keep scan receipts elsewhere
within STORE: the scanner refuses receipts under any scanned file's parent.

```
python3 tools/flock_privacy_scan.py DOCUMENT --json-out RECEIPT
python3 tools/flock_redact.py propose STORE SLUG SHA DOCUMENT --scan-receipt RECEIPT
python3 tools/flock_redact.py payload STORE SLUG SHA DOCUMENT > PRIVATE-PAYLOAD
# Human: inspect every proposed locator in the private original AND the blind spots.
# Human: read PRIVATE-PAYLOAD before signing with a key agents cannot use.
ssh-keygen -Y sign -f YOUR-KEY -n deflock-approval PRIVATE-PAYLOAD
python3 tools/flock_redact.py signoff STORE SLUG SHA DOCUMENT --signature PRIVATE-PAYLOAD.sig
python3 tools/flock_redact.py status STORE SLUG SHA DOCUMENT
python3 tools/flock_redact.py apply STORE SLUG SHA DOCUMENT
```

A scan with matches exits 3; that is evidence for review, not approval. Propose
requires a current full-category receipt with no allow file and no scan failure.
The receipt hash, not its text or matched values, is recorded in the proposal.
The document id is its source hash. Canonical JSON (sorted, indented, final newline)
is signed byte for byte. Signoff records only the verified principal; no typed
name, option, environment setting or unattended path can sign. Existing records
are never replaced. Re-signing a changed original requires a new source identity.

Apply freshly checks the source, proposal spans and human signature. It builds a
private staging store under STORE, including the derivative and eligibility record,
and requires the unchanged reader to return `(eligible, ok)` there **before** any
live output is created. Only the derivative location field is then relocated to
its final absolute path. Immutable links promote the derivative and record; the
reader and source are checked again, with rollback of our new links on failure.
All staging is removed on success or failure; existing live files are never deleted
or overwritten. An interrupted process can leave new files, never approval: inspect
and reconcile rather than overwrite them. `derivative_missing` is not permission
to write live outputs. Staged validation, not that reason, authorizes promotion.

The derivative is `SHA.redacted.txt` beside DOCUMENT, mode 0600; the original stays
byte-identical and private. Each approved span is removed, including overlaps and
cross-line spans, and replaced with category/byte-length markers, never a value or
value hash. Status reports none/proposed/signed/applied/stale using reader reasons;
no-match proposals report proposed (`not_required`), not signed or applied.
Only the redacted derivative may proceed to later publication review. Nothing in
this workflow publishes, and every existing human publication gate still applies.

## Blind spots

The shared `flock_privacy.py` patterns cover email, NANP separated phone numbers
and common uppercase California plate formats. Bare digit runs, unbracketed
spelled-out email, other plate formats, names, addresses, image text and visual
page redaction are not covered. Scan receipts have line/category only, not spans.
Pattern matches and a clean scan are not human privacy or legal review. All
fixtures are synthetic, and original text/quotes never enter refusal output.
