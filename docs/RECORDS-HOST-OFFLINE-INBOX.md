# Host offline inbox ingress

This is one inert host-launcher ingress option, not activation or substantive
review. Legacy schema-1 mailbox profiles remain unchanged, including their
existing approved network, mail configuration and version/containment gates.

## Explicit owner profile selection

An offline schema-1 profile retains all generic release, security, resource,
journal, data-directory and mount fields. Remove `mail_config` and its mount.
Add exactly these selector fields:

```json
{
  "input_mode": "inbox",
  "inbox": "/synthetic/private-inbox",
  "inbox_manifest_sha256": "<owner-computed 64-lowercase-hex manifest>",
  "network": {"mode": "none", "container_id": null}
}
```

The example is a schema fragment, not a provisioned profile. No mailbox
credential source, provider endpoint/model, paid key or spending reservation
is required. Unknown fields/arbitrary worker arguments, dual/neither selectors,
invalid hashes and non-none offline networks fail closed. Explicit
`input_mode: mailbox` with the legacy `mail_config` is also accepted; omitting
input_mode preserves the legacy mailbox contract.

The inbox requires an exact read-only mount at its container path, not merely
a read-only release ancestor. A writable parent with an exact read-only child
is supported when backing sources are disjoint. Profile and runtime checks
honor the deepest effective mount and reject descendant overlays and competing
input-source aliases. One named volume may supply multiple strictly disjoint
subpaths, including writable data and read-only input/parser paths. Equal,
ancestor/descendant or whole-volume aliases of the input are rejected regardless
of their read-only setting. Runtime configured subpaths must match the profile;
realized sources must be consistent with the same volume base or resolved
subpaths. Unbound or cross-kind overlapping sources fail closed.
Bind source ancestors/descendants cannot alias the input.
Only mounts covering required profile paths are accepted in offline mode, so
an unrelated credential mount is not needed or accepted.

Runtime inspection checks realized Mounts and configured HostConfig.Mounts:
exact target, bind source or volume name, read-only state, volume-nocopy and
the exact requested volume subpath. In-container UID/path preflight additionally
checks the actual read-only filesystem flag on the inbox and every input file.

## Manifest and provenance

The inbox is a flat owner0700 directory containing only owner0600, one-link
regular lowercase `.eml` files. No symlinks, nested directories, control-character
names or special files. Limits are 200 files, 64 MiB per file, 256 MiB total.
An empty inbox is valid. Hashing is bounded and reads bytes as data only:
no email-driven commands, callbacks, endpoint selection or MIME interpretation.

The owner can compute the stable filename/size/content manifest without Docker,
mailbox/provider configuration or ledger writes:

```sh
python -m campaign_tool.records.container_host inbox-manifest --inbox /synthetic/private-inbox
```

Only schema/status/hash/count/bytes are printed, never filenames or contents.
Put the hash into the owner0600 profile before launch. The existing immutable
profile SHA binds this expected manifest, selected mode/path and mounts to each
full CID/job receipt and cancellation fence. Bind directory identity, owner,
mode and metadata are anchored without following symlinks before create and
rechecked before admission. Host-readable owner input additionally receives
byte/stat snapshot checks. A rootless worker's UID 1000 may map to a different
host UID: the host uses no-follow final-directory metadata, never permission
changes or an ownership bypass. Its ancestors must remain host-accessible,
trusted and non-writable; inaccessible ancestry fails closed. The actual
worker UID, owner0700 directory, owner0600 files, read-only filesystem and
complete expected manifest are mandatory runtime checks before admission.
Named-volume bytes use the same runtime checks. The worker entrypoint rechecks
the full manifest/read-only state immediately before exec. Provisioning correctly
mapped ownership and any kernel read-only mounts is a separate owner operation.

Workers cannot mutate originals through their read-only mount or a second
writable source alias. Trusted host/Docker owners must keep the input snapshot
immutable for the job; their privileged modifications cannot be fenced by
unprivileged application code. No source snapshot is copied into the ledger
as a substitute for real intake.

## Fixed command and no-paid meaning

The entrypoint forwards only the validated selected path:

```text
python -m campaign_tool.records run --root PRIVATE_ROOT --inbox PRIVATE_INBOX
  --unattended --max-originals-per-run 200 --ocr --json
```

The accepted worker supervisor, CID-before-start/admission, receipt/cancel,
clean env-i, exact release/image/version/dependency binding and post-run gates
remain in place. No stop-post changes or native-tool/model installation is
performed. Images lacking the new host probe/entry contract fail readiness;
there is no compatibility downgrade or arbitrary argument forwarding.

The positive synthetic test invokes the real records CLI into an empty
disposable root with a genuine EML message and the unchanged --ocr command.
It supplies the existing synthetic OCR tool-metadata contract for text-only
input and forbids image OCR execution; missing discovery has separate refusal
coverage. The original frozen test incorrectly disabled discovery and failed
before intake; its failed evidence remains preserved. Superseding test receipts,
not this description, establish the repaired candidate's observed acceptance.
Raw PDFs do not belong directly in this inbox: text/raster PDFs must be genuine
EML attachments. Standalone document/PNG routes are separate gaps, not implicitly
covered. Real native OCR readiness, mapped UID access, read-only kernel mounts
and Docker lifecycle acceptance remain independent host integration gates. Existing fake-Docker
lifecycle/legacy regressions remain required. No real records, paid calls,
live jobs, schedules, image changes or credentials are involved.

Operational/mechanical completion never certifies substantive review:
provider-none reports remain `substantive_complete: false` with independent
source review queued for nonempty records. No automatic public findings.

Sources: [Docker volumes](https://docs.docker.com/engine/storage/volumes/) and
[Engine inspect API](https://docs.docker.com/reference/api/engine/version/v1.51/).
