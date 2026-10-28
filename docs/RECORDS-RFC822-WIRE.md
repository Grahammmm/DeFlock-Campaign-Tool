# Bounded RFC822 wire payload sidecar

`campaign_tool.records.intake.wire_rfc822.capture_rfc822_payload` is a pure,
standard-library helper for later intake integration. It reads no files, opens
no network connections, sends no mail, and changes no ledger or service.
`mail_delta.py` and `eml_export.py` are deliberately unchanged. The helper is
not enabled in intake and does not remove their current RFC822 coverage gap.

```python
from campaign_tool.records.intake.wire_rfc822 import (
    capture_rfc822_payload, WireRFC822Error,
)

# raw_bytes is an already-preserved, bounded outer MIME message.
try:
    original_eml = capture_rfc822_payload(raw_bytes, "1.2")
except WireRFC822Error as error:
    reason_code = str(error)  # Safe code only; retain the source privately.
```

## Identity and locator contract

The root locator is `1`. Multipart children are numbered in wire order starting
at one: `1.1`, `1.2`, `1.2.1`. Locators are canonical, not filenames, filesystem
paths, exporter walk indices, shortened aliases, or receipt identities.
Malformed locators fail with `invalid_locator`; nonexistent or below-opaque
locators fail with `unlisted_locator`; non-RFC822 targets fail with `not_rfc822`.
Named attachments and unnamed RFC822 nodes work identically. Headerless
`multipart/digest` children inherit `message/rfc822` as stdlib does.

Absent CTE, `7bit`, `8bit`, and `binary` on the RFC822 wrapper are identity
encodings: the returned bytes are the exact encapsulated EML wire range.
CRLF/LF, header folding, whitespace, binary body bytes, and existing terminal
newlines are preserved. Exactly one LF or CRLF immediately preceding an
enclosing delimiter belongs to MIME framing and is excluded. Two such newlines
leave one in the EML. A root RFC822 wrapper has no enclosing delimiter and
retains all bytes after its header separator. Nothing is reserialized; an
`EmailMessage.as_bytes()` result must never be labeled the original.

Wire ranges and validated headers are built first. A comparison copy replaces
only RFC822 bodies with a fixed synthetic leaf EML before stdlib parsing.
Every outer node header, media type, ordered child count and hierarchy must
match the stdlib tree. RFC822 nodes are opaque: stdlib never receives their
original bodies, and their synthetic comparison child is not a source locator.
Only the encapsulated top header block is validated. Its MIME body (including
nested attachments or malformed/deep multipart content) is preserved, not
interpreted. Callers can deliberately call the helper again on captured bytes
with a separate budget if they need a locator inside that message.

Stdlib header-only parsing reports `MultipartInvariantViolationDefect` because
no body was parsed. Only that expected defect is excluded for already-validated
multipart headers. Other message/header defects remain fatal, and full-body
comparison still requires zero parser defects. Comparison-parser test mocks
are isolated from the independent header-only parser.

## Bounded fail-closed subset

- `max_bytes`: default and ceiling 64 MiB of input; nonempty immutable `bytes` only.
- `max_depth`: default and ceiling 32, counting outer root as depth one.
- `max_parts`: default and ceiling 1,000, including root and opaque RFC822 wrappers.
- Keyword limits may only be lowered to positive integers; booleans are invalid.
- Each traversed or encapsulated top header block is at most 64 KiB; each physical header line is at most 998 bytes excluding LF/CRLF.
- Header bytes must be ASCII printable characters or horizontal tabs, with an explicit LF/CRLF blank separator, valid field names and non-orphan folds.
- Structural Content-Type, CTE, Content-Disposition and MIME-Version headers must be unique and defect-free. If present, MIME-Version must be `1.0`.
- Content-Type accepts an explicit token/token and unambiguous token or quoted-string parameters, including folded lines. Comments and duplicate parameters are unsupported; extended `boundary*` parameters are refused.
- Multipart boundaries must be valid 1-70 character ASCII MIME boundaries with no trailing space; every traversed multipart needs opening and closing delimiters and nonempty child ranges.
- Reused or prefix-related boundary tokens anywhere in the traversed outer tree are conservatively refused, even in otherwise valid disjoint siblings. Tokens inside opaque RFC822 bodies are not inventoried.
- Delimiter lookalikes, consecutive delimiters, additional delimiters after closing, framing errors, and stdlib mapping disagreements fail closed. Normal preambles, epilogues, delimiter whitespace and an EOF closing delimiter are supported.
- Multipart and RFC822 wrappers accept only identity CTEs. Base64, quoted-printable and unknown RFC822/container encodings are not decoded and are refused.
- Scalar outer siblings may declare base64 or quoted-printable; no extraction or decoding of those siblings is claimed. Unknown scalar CTEs and other `message/*` node types are refused.
- The encapsulated EML needs at least one From, Subject or Date header. Its scalar CTE is preserved, never recursively decoded.

Depth/part validation precedes stdlib full-tree parsing. Limits do not count
opaque descendants because they are not parsed; all their bytes still count
toward the input cap. Memory is proportional to the bounded input, comparison
copy and parsed tree, with at most one fixed synthetic replacement per opaque
node; this is not streaming and is not a strict process-memory cap. Header
validation and repeated boundary scans cost up to input size times bounded
outer depth. There is no OCR, archive extraction, charset conversion, MIME
repair, full RFC grammar support, cryptographic authentication or provenance
attestation.

## Integration owner and independent review

Return bytes to the later integration owner. That owner must preserve the outer
original, bind exact locators and hashes, reconcile exporter behavior and
receipt manifests, and independently account for every required attachment.
This helper checks a requested locator, **not** unlisted receipt attachments
or nested attachment completeness. Preserving an opaque EML is not extracting
all its attachments. No production deployment or readiness is asserted.
The owners of the separate mail-delta/export work retain integration and merge
responsibility. Parser failures remain explicit owner-review items, never
silent reserialization fallbacks.

## Synthetic verification

```sh
python3 -B -m unittest tests.records.test_wire_rfc822 -v
python3 -B tools/check_public_tree.py --patterns-only
node scripts/scan-secrets.mjs
```

The suite includes byte identity where stdlib serialization changes folds and
line endings; CRLF/LF and mixed envelopes; named/unnamed and digest nodes;
nested outer multiparts and opaque inner multipart roots; binary identity
payloads; duplicate/prefix boundaries; malformed types, encodings and headers;
framing and mapping disagreements; unlisted/path-like locators; and size,
header, depth and part limits before full-tree parsing. All inputs are generated
synthetic bytes. Existing offline CI discovers the new tests without workflow
edits. Passing synthetic tests and pattern scans do not substitute for
independent security/privacy review or guarantee every secret format is caught.

Framing and identity-encoding references:
[RFC 2046 sections 5.1.1 and 5.2.1](https://www.rfc-editor.org/rfc/rfc2046)
and [RFC 2045 section 6](https://www.rfc-editor.org/rfc/rfc2045).
