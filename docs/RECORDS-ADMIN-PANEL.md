# Records admin panel (`records panel`)

A private, local, stdlib-only dashboard over the records pipeline. It **sends nothing, publishes nothing and runs
nothing**. Approving a draft only marks it approved-to-send in the approval queue; a human sends separately. It is
not installed, enabled or started by this repository.

```sh
records panel --project PROJECT --run-id RUN_ID [--run-report FILE] [--kit-root KIT] [--as-of YYYY-MM-DD] \
              [--port 8765] --actor LABEL [--timer-unit records-run.timer] [--no-system]
records panel --project PROJECT --run-id RUN_ID ... --render out.html     # static snapshot, no socket
```

## Roots, run and report are distinct

- `PROJECT` holds `processing-v1/ledger.sqlite` (canonical ledger), `run-outputs/RUN_ID/` (the selected run's
  outcomes) and `approvals/queue.jsonl` (the queue). The runtime directory of `run-records.sh` is **not** the project:
  pass its `reports/RUN_ID.json` (or `run-report.json`) explicitly with `--run-report`; nothing is guessed from mtimes.
- `RUN_ID` must be a literal safe id; a missing run shows as unavailable, never as an empty success.
- The page labels current ledger state and the selected run's outcomes as separate sources.

## Sections (one page, `GET /`)

| id | source |
| --- | --- |
| `mail` | ledger `mail_messages`; attachment counts only from ledger occurrences |
| `documents` | ledger originals (corrected effective fields), per-document stage state, blocked stages by recorded cause, page state |
| `stages` | validated run report (the eight orchestration stages, `needs_g` unchanged), canonical seven evidence stages, `schedule.health` (last run, status), timer state (`systemctl --user is-active`, read only) and disk |
| `findings` | `run-outputs/RUN_ID/outcomes/received-items.json`: agency/request/item ids, page + quote, rule/policy refs, documentary rating |
| `deadlines` | `kit_track.due` when `--kit-root` is given (overdue first, undated last); plus dates stated in received mail, passed first |
| `drafts` | approval queue projection with audit line (last actor and time, revision, creation time) |

Each source fails on its own with a fixed code (`ledger_missing`, `report_invalid_json`, `received_oversize`,
`queue_invalid`, ...). Missing, skipped, held or unknown is never shown as passed. `gap` is documentary, not a legal
finding; a blank findings list means none reported in that source and run, not compliance. Dates stated in mail and
kit dates are provisional and are not a legal deadline calculation. Mail text, quotes, reasons and drafts are escaped
plain text; no document-supplied link is clickable. The panel reads no raw `.eml`, document bytes or portal links.

## Drafts and decisions

Use **Edit** to remove draft banners, `# Draft (` headings, `[EDIT ME]` placeholders,
reviewer notes, `BEFORE SENDING` instructions and template footers before approving.
Placement and sending refuse remaining residue with `body_has_draft_residue`;
approval alone does not make a working draft ready to send.

- **Approve** follows the approval queue: the form shows the exact `approval_payload` bytes; sign them elsewhere
  (`ssh-keygen -Y sign -n deflock-approval`) and paste the signature. The panel holds no key and never signs.
  Result: `approved`, marked approved-to-send, nothing sent. A typed label never approves.
- **Reject** needs a reason; **Edit** saves new text (the queue returns it to pending; it is never auto-approved).
  Both record the startup `--actor` label, which is local single-operator attribution, not authenticated identity.
- Approved and rejected drafts are read-only here. The decisions file is the queue's append-only journal; the panel
  adds no second store. State is reloaded on every GET and success is confirmed by reading the queue back.
- No stale-revision protection beyond the queue's own state rules; concurrent CLI edits keep upstream semantics.

## Local-only model

Bound only to literal `127.0.0.1` (no host option). Every request must carry `Host: 127.0.0.1:PORT`; POSTs also need
`Origin: http://127.0.0.1:PORT` and the per-start CSRF token (in hidden form fields only, never URL or logs).
To reach it from another tailnet device use an SSH/`tailscale` port forward to the **same port** so the Host header
matches; the panel never listens on another address. The token stops cross-origin browser requests; it does not stop
another process of the same account that can already read these files (trusted local account). Only `GET /` and the
three `POST /approvals/{approve,reject,edit}` routes exist; bodies are urlencoded UTF-8, at most 256 KiB, with a
read timeout. Responses are `no-store` with a strict CSP; request logging is off.

`--public-origin https://admin.example.test` adds exactly one case-sensitive Host/Origin pair:
`Host: admin.example.test` with POST `Origin: https://admin.example.test`. Loopback remains accepted,
but the pairs cannot mix. The hostname must be lowercase DNS with at least two labels; no port,
path, query, fragment or trailing dot is accepted. Missing or duplicate Host/POST Origin is refused.
The bind stays literal `127.0.0.1`; `--render` ignores this flag.
This is only safe behind Cloudflare Access with **cloudflared Access JWT validation**.
`--access-config FILE` is required with `--public-origin` (and refused without it): an owner-only (0600, regular,
non-symlink, at most 4096 bytes) JSON object with exactly `aud` (64 lowercase hex), `email` and `team_domain`
(`<team>.cloudflareaccess.com`). Every request on the public Host must then carry one `Cf-Access-Jwt-Assertion`
whose header is RS256 and whose `aud`, `iss`, `exp`, `iat` and `email` claims match; otherwise it gets the generic 403.
This check does **not** verify the signature: alone it is only a tripwire against a missing or misconfigured edge, never
authentication. Loopback requests are exempt. Never commit the real values.
Never add an edge rule that rewrites Origin. Existing CSRF and action checks still apply.

## Static snapshot

`--render out.html` writes the same view with no forms, token or endpoint links, banner "Static snapshot — decisions
unavailable". Output is owner-only 0600, replaced atomically in an existing symlink-free directory; it refuses
symlink/FIFO outputs and any path that is a source file. `--no-system` skips the timer and disk checks for
deterministic output.

## Errors

Unknown draft 404, invalid transition 409, missing/invalid signature 403, malformed source 503; failed requests append
nothing and reflect no input.

## Next steps

The `/next-steps` page lists recommended social, site and follow-up drafts with Approve and Deny; see [RECORDS-NEXT-STEPS.md](RECORDS-NEXT-STEPS.md). Static `--render` includes the same sections without forms.

### Manual events with a read-only snapshot

Use `records panel --manual-events DIR` to keep manual form events outside the
project. DIR must already exist, be absolute, owned by the running uid, have no
group/other permissions (for example 0700), and not be a symlink. Its real path
must neither contain nor be inside the project. Events append to
`DIR/manual-events.jsonl` under `DIR/manual-events.lock`; nothing in the project
is rewritten. The confirmation links back to the agency; the event appears on
the timeline after the next data refresh. Snapshot-builder merging of this
journal is staged separately in CDATA-MANUAL-MERGE-1. Without the flag the
existing project journal and immediate timeline refresh remain unchanged.
