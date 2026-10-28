# Genuine standalone document drop

This candidate adds a separate local PDF/XLSX intake route. It does not wrap
standalone documents in email or infer agency, request, acquisition date, or
portal provenance from filenames.

## Fixed contract

Create an owner-only flat document directory: directory mode 0700, regular
single-link files mode 0600, owned by the worker. Supported initial suffixes are
PDF and XLSX; the bytes must match the selected format. The bounds are 200 files,
64 MiB per file and 256 MiB total. The originals are never modified.

Generate the bounded hash manifest with:

~~~sh
python3 -m campaign_tool.records.container_host document-manifest --documents /private/drop
~~~

The host profile uses exactly one input selector. Legacy mailbox profiles stay
unchanged. Document profiles replace mail_config with input_mode "documents",
documents (the exact container path), and document_manifest_sha256. They require
network mode none and an exact read-only input mount, not an arbitrary read-only
ancestor. Deepest effective coverage, worker ownership, stable source identity
and competing source aliases remain fail-closed. No mailbox configuration or
secret mount is required by document mode. Existing release, job, cancellation,
CID and private receipt guards are unchanged.

The fixed inner command is:

~~~sh
python3 -m campaign_tool.records run --root /private/records --documents /private/drop --document-manifest-sha256 MANIFEST --unattended --max-originals-per-run 200 --ocr --json
~~~

The CLI requires the document path and manifest together and rejects combining
them with inbox or mailbox intake. No record content selects arguments, adapters,
or callbacks. A writable input, mutated bytes, wrong owner, symlink, hardlink,
missing file, over-limit input or inconsistent manifest is rejected.

## Preservation and limits

Actual standalone bytes are sealed into the shared content-addressed store.
A captured local acquisition receipt binds source path, manifest, file identity,
hash, byte count and verified format. The canonical occurrence kind is local;
no mail message or fabricated parent is registered. The trusted preservation
adapter verifies every mail, attachment and local occurrence, including
shared-byte originals acquired through more than one route.

Per-run intake attempt and time budgets include failures and replays. Durable
progress is bound to the unchanged manifest, account and exact input root. A
capped run resumes at the next verified entry rather than charging the first
sorted replay forever. A changed manifest starts a new sweep. Failed attempts
do not advance progress or clear existing safety holds. Malformed formats
retain their exact CAS bytes but do not receive successful canonical
preservation. Existing extraction dispatch processes PDF pages and XLSX sheets.
Image-only PDF pages need the already configured local OCR runtime; missing OCR
or partial coverage remains a gap. This does not resolve standalone image
support or establish whole-format-corpus acceptance.

Substantive independent source review remains queued. Deterministic extraction
and detectors are not a full review, human approval or legal certification.
Document mode does not configure models, providers, spending or credentials.

## Evidence boundary

The new tests use generated documents, fake read-only mount observations and a
captured worker exec command. They exercise genuine CLI intake and canonical
registration, not a prepopulated ledger. Passing synthetic tests do not prove
actual host mount ownership, installed OCR availability, real document
provenance, image readiness or integrated scheduled-job operation. No live
document processing or host launch is authorized by this source change.
