# WP0 release identity and installation

The engine now exposes one records CLI while preserving the existing four gate
commands. `python3 -m campaign_tool.records version --json` reports the source
commit, package version, canonical ledger schema version, dependency-lock hash,
code features and whether the source is dirty or tagged. `records` is the same
entry point after installing a wheel. Schema version 0 explicitly means the
canonical seven-stage ledger is absent; it does not imply migration success.

## Candidate and release identity

A version number is not proof of a release. Untagged or dirty source is reported
as a candidate. A clean checkout tagged exactly with the package version is
reported as a tagged release. The owner merges this package's PR, then creates
the release tag on the reviewed successor commit. Do not tag an older tree that
does not contain the manifest/CLI implementation, or move an existing tag.

A wheel can be built from clean candidate source for review. The custom build
step embeds the source manifest, exact package-file hashes, and the bytes of
requirements-records-test.txt. Installed `records version --json` verifies the
package inventory and dependency-lock bytes without Git or a network connection.
It rejects changed or extra package files and mismatched versions. This is an
integrity check, not a cryptographic signature or owner publication approval.
The lock pins optional parser and scanner versions; base runtime uses the
standard library. The report does not claim those optional packages are installed.

## Build and isolated install

Use an approved environment with the pinned requirements-build.txt packages.
No system package install or service restart is part of this procedure.

```sh
python3 -m pip wheel --no-deps --no-build-isolation --wheel-dir "$WHEEL_OUTPUT" .
python3 -m venv "$CANDIDATE_RUNTIME"
"$CANDIDATE_RUNTIME/bin/python" -m pip install --no-deps --no-index "$WHEEL_OUTPUT"/*.whl
cd "$ISOLATED_CHECK_DIRECTORY"
"$CANDIDATE_RUNTIME/bin/records" version --json
```

If the target host lacks pip/venv support, build and install into an isolated
staging target using an approved bundled build runtime, then transfer the pure
Python package with its wheel hash and manifest. Do not change the container
image or system Python to work around missing tooling. Keep candidate and active
runtime paths separate; changing the active runtime remains an owner action.

## Public-tree scan

`records scan-public --patterns-only --root "$PUBLIC_CHECKOUT"` runs every
credential and strict path/portal pattern over tracked and nonignored files.
CI runs this public-only check and synthetic private-index regressions. Its
result always says `private_export_clearance: false`; it cannot certify the
private corpus. Existing credential patterns and credential-scan jobs remain
enabled. Exit 1 means findings; exit 2 means incomplete/invalid scanning.

### Private exact-export gate

Prepare a separate export directory outside any Git checkout. Do not send it
until this private-side gate succeeds; the CLI performs no export or upload:

    records scan-public --strict --root "$EXACT_EXPORT" \
      --private-index "$PRIVATE_INDEX" --attestation "$PRIVATE_RECEIPT"
    records scan-public --strict --root "$EXACT_EXPORT" \
      --private-index "$PRIVATE_INDEX" --verify-attestation "$PRIVATE_RECEIPT"

Strict mode requires the index and either creation or verification of an
attestation. It walks every export entry, including hidden and ignored names,
and scans the same bytes it hashes. Symlinks, Git metadata, special files,
non-UTF-8 content and common archive containers fail closed. This source-tree
gate does not certify wheel/archive contents; those need a separate reviewed
artifact adapter. UTF-8 transport encodings and paraphrases are not decoded or
semantically recognized. Manual privacy review is still required.

The owner-only index and receipt must be outside the export and all Git
checkouts. Neither belongs in a public artifact, PR, workflow secret or GitHub
upload. The immutable receipt binds the exact relative-path/type/mode/byte-count/
SHA-256 inventory, index bytes, declared coverage and scanner code. Changes to
any binding invalidate verification. Freeze the export and reverify immediately
before a separately authorized handoff; no send/push adapter is implemented
here. An attestation is a scan receipt, not a signature or publication approval.

The index keeps `original_sha256`, `portal_hosts`, and optional `text_fragments`
arrays. Strict mode additionally requires:

- `coverage`: exactly `schema_version` (integer 1), `inventory_sha256` (the
  private inventory receipt hash), `originals_complete`, `portal_hosts_complete`,
  `correspondence_complete` (all true), and `correspondence_source_sha256`
  (the complete declared correspondence-source hash list).
- `correspondence_sources`: one object with exactly `source_sha256` and `text`
  for every declared correspondence source. Hashes must also occur in
  `original_sha256`; missing/duplicate sources and empty normalized text fail.
  Text is the private full-text index, never a public fixture or an upload.

The private index producer/reviewer must substantiate the coverage declaration
against the referenced inventory and extracted originals. This scanner checks
the declaration's consistency, not the truth or authenticity of that external
inventory. Unsupported or unindexed sources must not be declared complete.
A hash-only index, partial coverage or missing correspondence sources cannot
produce clearance. An explicitly complete inventory containing no correspondence
can use empty correspondence lists.

Correspondence matching normalizes Unicode, case and whitespace and detects
every 24-character window of the supplied full texts (short texts are matched
in full). Resource-limit exhaustion blocks clearance rather than dropping
sources. Additional literal fragments remain checked. This covers indexed
literal excerpts, not all possible transformations or contextual disclosures.

A zero-byte file matching the empty original hash is reported under
`non_content_matches` as `zero_byte_original_non_content`, not as proof of a
private document leak. This exception concerns only the empty byte stream.
Nonempty byte-identical originals, printed private hashes (including the empty
hash), denied paths, correspondence and credentials still block. No finding is
waived because another match was classified non-content.

## Acceptance and owner release action

`test_wp0_acceptance` exercises the version CLI, old gate commands and strict
scanner entry point. Unit tests cover manifest integrity, tampering and private
index detection. The release workflow builds a wheel, installs it outside the
checkout and compares its commit with the CI checkout SHA on Python 3.11-3.13.
Run the existing full tests and both credential scans before pushing. Record the
current CI run ID, wheel hash, installed manifest and exact commit in the private
WP0 report. Preserve the historical baseline result separately.

After owner merge, the owner creates tag `0.1.0` on that reviewed commit and
approves the active pinned runtime. Rebuild from the tag and repeat the installed
version check. Until those actions and matching host evidence exist, report
WP0 as candidate or merged-but-not-deployed. No scheduler is enabled, original
is processed, review is approved or website content is published by WP0.

### Committed package boundary

Clean provenance additionally requires every package input hash to match the
package blobs archived directly from HEAD. Ignored/untracked additions, missing
files and hidden tracked changes therefore remain candidates even when Git
status is clean. Wheel builds reject them and compare the built package inventory
against that committed manifest before embedding provenance. Existing build
output cannot silently add stale package code.
