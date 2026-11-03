# Private IMAP config: reusable JSON credential sources and fetch budget

The engine can read one literal field from an existing, flat JSON exporter
credential file. The file does **not** need to be converted into engine config.
All configuration and credential files stay outside the public repository.
This feature does not enable intake, install tools, change schedules or edit
production settings.

## Nonsecret example

An organizer-owned engine config at a generic private path such as
`/private/organizer/mail.json` can contain:

```json
{
  "host": "imap.example.invalid",
  "username": "synthetic-user",
  "account_id": "synthetic-account",
  "folders": ["INBOX", "Agencies"],
  "password_source": {
    "type": "json",
    "path": "/private/organizer/exporter-env.json",
    "key": "IMAP_PASSWORD"
  },
  "max_messages_per_run": 200
}
```

The existing source is a flat object whose field names may be environment
variable names. `key` is a literal top-level field name, not JSONPath, a dotted
traversal or an environment lookup. Only a nonempty string is accepted, and
password whitespace is preserved. Other fields are not imported as engine
settings. No credential values are included in this example.

Relative source paths resolve beside the engine config, not the working
directory. The source mapping must contain exactly `type`, `path` and `key`,
with `type: "json"`. Combining `password_source` with any of `password`,
`password_file` or `password_env` is rejected, even if the other field is empty.
Existing configs without `password_source` retain their inline-password,
password-file, then environment-variable precedence. Legacy password files
continue to strip surrounding whitespace.

## Source protections and error hygiene

The config and JSON source are opened with `O_NOFOLLOW` and `O_NONBLOCK`,
checked through the open descriptor for regular-file type, current UID ownership
and no group/other permission bits, and read with a 64 KiB limit. Owner-only
modes such as 0600 and 0400 are allowed. Symlinks at the final path component,
directories, FIFOs, oversized inputs and duplicate JSON keys are refused.
A before/after `fstat` compares device, inode, mode, owner/group, link count,
size and nanosecond modification/change times. Invalid JSON, nonfinite values,
missing keys and wrong value types fail closed.

Use trusted parent directories. `O_NOFOLLOW` protects the final component, not
symlinked ancestors; this is not an openat-based whole-path sandbox. Metadata
checks detect ordinary concurrent mutation but do not replace trusted-owner
storage. Legacy `password_file` behavior is retained rather than represented as
having these new JSON-source protections.

Errors use stable codes, never source paths, source values or parser/OS error
text. Fetch/preserve and connection errors do not echo underlying exception
messages. `redacted()` uses an operational-field allowlist; password values,
source descriptors and arbitrary extra fields never enter the fingerprint.
Credential rotation does not change the fingerprint. The effective fetch limit
does, including the new default; historical run identity hashes may consequently
differ after upgrade.

## Global attempted-fetch budget

`max_messages_per_run` defaults to **200** for existing configs. It must be an
integer from 1 through 200; booleans, strings, floats, zero and higher values
are rejected. For a small synthetic batch, set `"max_messages_per_run": 2`.
The same validation applies to programmatic intake configs.

The budget is shared across every configured folder, in configured order (or
sorted server order when all folders are selected). Each attempted FETCH consumes
one slot **before** it happens, including failures and UIDVALIDITY replay.
No folder gets a separate 500-message allowance. Once exhausted, later folders
are still listed, selected and searched for visibility, but not fetched.
This bounds fetched messages, not LIST/SEARCH response size or downstream work.
A perpetually busy earlier folder can delay later folders; configure order
deliberately. There is no scheduling or fairness change in this slice.

Reports include global and per-folder `attempted`, `deferred` and
`limit_reached`, plus the effective global `max_messages_per_run`.
`new` is the full searched candidate count. `deferred` counts candidates not
attempted, whether due to budget exhaustion or an earlier folder failure.
The failed attempted UID is counted as a failure, not deferred, and is retried.
Global `limit_reached` means the attempted count equals the limit, even if no
backlog remains; a folder's flag means it has deferred candidates when the global
limit has been used. A failed selection/search cannot provide a deferred count,
and remains an explicit folder failure.

Only fully preserved UIDs advance the checkpoint. Deferred UIDs never do.
New/reset folders can still establish a zero checkpoint without fetching.
Subsequent runs resume deferred/failed candidates from the durable checkpoint.

## Synthetic checks only

```sh
python3 -B -m unittest tests.records.test_imap_intake tests.records.test_imap_config_sources -v
python3 -B -m unittest discover -v
python3 -B tools/check_public_tree.py
node scripts/scan-secrets.mjs
```

The dedicated IMAP CI workflow uses standard-library tests and installs nothing.
No real source file, mailbox or production config is needed to test this feature.
Passing tests do not authorize a live intake or constitute independent review.

## Trusted failure-code observability

Fetch/preserve failures retain reviewed literal codes only from the exact
project error classes `IntakeError` and `mail_delta.Rejected`. The fixed
allowlist includes `ambiguous_inline_body_part`, `unsupported_rfc822_part`,
`export_scope_mismatch` and reviewed resource/receipt integrity codes.
Unknown codes, arbitrary exception classes, subclasses, nonstring or multiple
arguments and arbitrary exception text remain `fetch_or_preserve_failed`.
Exception formatters are not called. A code reports a failure classification,
not source content or a credential. Failed attempts still consume the global
budget and do not advance checkpoints; the existing keyed alert identity is
unchanged. Extending this allowlist requires review, not a pattern-based trust
rule or blanket acceptance of a project's exception messages.

## Poll step: `records mail-poll`

`python3 -m campaign_tool.records mail-poll --config C --store S [--dry-run]` reads one
folder once and exits: connect, list, fetch (BODY.PEEK) what the cursor has not seen, hand
each message to intake (`eml_export`), write a `records-mail-poll-v1` receipt, then advance
`S/mail-poll/state.json` once, atomically (0600 temp + `os.replace`). It prints one line,
`mail-poll | new=N already=N conflict=N notices=N skipped=N | review-required; nothing sent`,
and exits 0, 1 on any refusal (`mail-poll | refused: <code>`; the cursor is unchanged), or 2
on usage. A crash before the cursor write re-fetches next run; intake writes are idempotent.

Credential rule: no agent handles the mailbox secret. `C` holds only `host`, `port` (993),
`user`, `password_file` and optional `folder`, `account_id`, `timeout`; both files are
absolute, outside the repo, regular, owned by the caller, mode 0600 and not symlinks. The
password is read at connect time and never stored, printed, logged or put in a receipt.
`tls` may only be `implicit`; plaintext or STARTTLS is refused (`tls_required`). Refusals
name the field (`config_mode_not_0600`, `config_field:password_file`, ...), never a value.
At most one reconnect follows a transport failure (`connection_refused`); `auth_refused`
and `tls_unavailable` are not retried.

The mailbox is selected read-only: nothing is sent, flagged, moved or deleted. Same
Message-ID and bytes counts `already`; same id, different bytes is a `conflict` held in
`S/mail-poll/quarantine/` and the first copy is untouched. A message with no attachment and
a portal `/documents/` link is a notice, not a received file. A message with an attachment
name containing `/`, `\` or NUL is held whole in quarantine with those parts refused
(`skipped`). `--dry-run` counts and writes nothing. No timer is installed here.
