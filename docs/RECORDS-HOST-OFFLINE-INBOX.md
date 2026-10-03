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
input-source aliases. A named input volume must be dedicated to this input,
not also mounted elsewhere (even with another subpath); an already provisioned
volume/subpath may be used. Bind source ancestors/descendants cannot alias it.
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
full CID/job receipt and cancellation fence. Bind-source bytes are checked
before create and their pinned metadata is checked before admission. Named
volume bytes are checked in-container before admission. The worker entrypoint
rechecks the full manifest/read-only state immediately before exec.

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

Synthetic tests exercise real records CLI intake into an empty disposable root.
That test stubs only native OCR discovery for a text-only fixture; real image
native-tool readiness, read-only kernel mounts, UID access and Docker lifecycle
acceptance remain independent host integration gates. Existing fake-Docker
lifecycle/legacy regressions remain required. No real records, paid calls,
live jobs, schedules, image changes or credentials are involved.

Operational/mechanical completion never certifies substantive review:
provider-none reports remain `substantive_complete: false` with independent
source review queued for nonempty records. No automatic public findings.

Sources: [Docker volumes](https://docs.docker.com/engine/storage/volumes/) and
[Engine inspect API](https://docs.docker.com/reference/api/engine/version/v1.51/).
